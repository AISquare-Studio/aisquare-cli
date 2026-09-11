"""Claude Code evidence: the five hooks that exist, and the pane that carries the rest.

This module decodes what the *current* CLI can observe about a Claude Code
session. It is deliberately narrower than P03's plan, because the plan describes
hooks this CLI does not install.

**What is installed.** ``core.agents`` registers exactly five lifecycle hooks —
``SessionStart``, ``UserPromptSubmit``, ``SessionEnd``, ``Stop`` and
``Notification`` — and ``install_hooks`` writes exactly those into
``settings.json``. ``PreToolUse``, ``PostToolUse``, ``AskUserQuestion``,
``ExitPlanMode``, ``PreCompact``, ``PostCompact``, ``StopFailure`` and
``SubagentStart``/``SubagentStop`` are **not installed**, and this packet may not
change hook installation. So the evidence kinds the plan lists under "Claude
evidence decoding" cannot be sourced from hooks at all: they are frame-only or
unavailable. :data:`NOT_INSTALLED_HOOKS` names them so a later packet reads a
list rather than rediscovering the gap — and so
``tests/office/test_provider_evidence.py`` can assert the split against what
``install_hooks`` actually writes.

**What a payload carries.** Across all five commands the CLI reads exactly nine
keys (:data:`PAYLOAD_KEYS`). None is guaranteed — every accessor in
``cli/hook.py`` degrades to ``None`` or ``{}`` — and none carries an option
label, an option key, the on-screen order, the current selection, a dialog kind,
a tool name or plan text. That is exhaustive rather than a sample, which is why
:func:`classify_dialog` exists and why ``detected_by`` is ``frame`` for every
option list this back-end produces. ``Notification.message`` is opaque free text
defaulting to ``"needs your attention"``, and is deliberately never classified
on: the frame carries the real dialog.

**What is deliberately not recorded.** The prompt text is already an egress
surface — ``services/hooks.py`` spools it to Explainability — and Office must not
widen it, so :func:`hook_facts` records the prompt's *length* and never its text.
``transcript_path`` and ``cwd`` are filesystem paths travelling toward a browser,
so neither becomes a fact: the transcript is recorded as present or absent, and
``cwd`` is dropped entirely. The sidecar makes the same choice and asserts that
the path bytes never reach the database file.

**What the frame classifier is validated against.** The option vocabularies and
the on-screen shapes below are taken from the pinned 1.4 contract artifact and
from the v1.3 mock server that renders "a prompt as the pane shows it"
(``tools/office-mock-server.mjs``). They are **not** validated against a captured
live dialog: every Claude pane on this machine runs its TUI on tmux's alternate
screen (measured: ``alternate_on=1``, ``history_size=0`` on all five live panes),
so there is no scrollback to mine and no dialog was on screen to capture. Until a
real dialog is captured, treat :func:`classify_dialog` as fitted to the contract's
own rendering rather than proven against the terminal, and prefer returning
``None`` over guessing — which is what it does.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from aisquare.office.models import (
    DetectedBy,
    HookFact,
    OptionConsequence,
    PermissionMode,
    PromptEvidence,
    QuestionKind,
    QuestionOption,
)

PROVIDER: Final = "claude-code"

INSTALLED_HOOKS: Final = (
    "SessionStart",
    "UserPromptSubmit",
    "SessionEnd",
    "Stop",
    "Notification",
)
"""The five lifecycle hooks ``core.agents`` installs, in install order."""

NOT_INSTALLED_HOOKS: Final = (
    "AskUserQuestion",
    "ExitPlanMode",
    "PostCompact",
    "PostToolUse",
    "PreCompact",
    "PreToolUse",
    "StopFailure",
    "SubagentStart",
    "SubagentStop",
)
"""Hook kinds P03's plan asks for that this CLI does not install.

Recorded rather than decoded. Sourcing any of them would mean changing hook
installation, which this packet is forbidden from doing, so the evidence they
would have carried is frame-only or simply unavailable.
"""

PAYLOAD_KEYS: Final = (
    "cwd",
    "session_id",
    "source",
    "transcript_path",
    "model",
    "effort.level",
    "prompt",
    "message",
    "stop_hook_active",
)
"""Every payload key read anywhere in the hook path. A payload may carry more;
they are ignored, because nothing in this CLI enumerates what Claude Code sends."""

DEFAULT_NOTIFICATION: Final = "needs your attention"
"""What ``hook_notification`` stores when the payload carries no message."""

HOOK_FACT_NAMES: Final = (
    "hook",
    "source",
    "model",
    "effort",
    "message",
    "prompt_chars",
    "stop_hook_active",
    "transcript",
)
"""The allow-list of fact names this module will emit. No path, no prompt text."""

MAX_TEXT_CHARS: Final = 400
"""``Question.text``'s own bound, applied at the point evidence is made."""

MAX_LABEL_CHARS: Final = 200
"""One option label. The schema leaves it unbounded; evidence does not."""

MAX_RAW_CHARS: Final = 2_000
"""Bounded pane text kept as evidence, matching the sidecar's ``max_raw_chars``."""

MAX_OPTIONS: Final = 8
"""``Question.options``' own bound."""

MAX_SCREEN_LINES: Final = 200
"""How much of one frame is ever scanned, so a huge pane cannot become the cost."""

_ANSI: Final = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]"
)

_OPTION: Final = re.compile(
    r"^(?P<marker>[\u276f>\u203a]?)\s*(?P<ordinal>\d{1,2})\.\s+(?P<rest>\S.*?)\s*$"
)
"""One rendered option row: an optional cursor marker, an ordinal, then the label.

The marker is the *cursor*, not a stored default: it is where the selection is
right now and it moves with the arrow keys. It is reported as
:attr:`PromptEvidence.selection` and never as ``QuestionOption.default``, because
"this is highlighted" and "this is the remembered default" are different claims.
"""

_TAB_HEADER: Final = re.compile(r"^\[[^\]]{1,12}\](?:\s+\[[^\]]{1,24}\])+\s*$")
"""An ``AskUserQuestion`` tab strip: ``[Fallback] [Submit answers]``."""

_FIELD_MARKER: Final = "▁"
_SUBMIT_HINT: Final = re.compile(r"Enter to submit", re.IGNORECASE)
_CONTINUE_HINT: Final = re.compile(r"press\s+enter\s+to\s+continue", re.IGNORECASE)
_PROCEED: Final = re.compile(r"do you want to proceed\?|would you like to proceed\?", re.IGNORECASE)

#: Label pattern → (option key, consequence). Taken from the frozen contract
#: fixtures and the mock's own renderer, which is the only rendering of these
#: dialogs anyone has agreed on. A label that matches nothing here keeps its
#: on-screen ordinal as its key, which is what a numbered list actually offers.
_PERMISSION_LABELS: Final[tuple[tuple[re.Pattern[str], str, OptionConsequence | None], ...]] = (
    (re.compile(r"allow all edits during this session", re.I), "allow-remember", "session"),
    (re.compile(r"don'?t ask again|do not ask again", re.I), "allow-remember", "remembers"),
    (re.compile(r"^yes$", re.I), "allow", None),
    (re.compile(r"^no[,.]", re.I), "deny", None),
)

_PLAN_LABELS: Final[tuple[tuple[re.Pattern[str], str, OptionConsequence | None], ...]] = (
    (re.compile(r"use auto mode", re.I), "approve", "mode_auto"),
    (re.compile(r"auto-accept edits", re.I), "approve", "mode_accept_edits"),
    (re.compile(r"manually approve edits", re.I), "approve-manual", "none"),
    (re.compile(r"keep planning", re.I), "revise", None),
)

#: Frame hint → permission mode. ``auto mode on`` is the one measured against a
#: live pane (all five running agents render ``⏵⏵ auto mode on``); the other three
#: are read from Claude Code's own mode vocabulary and are unconfirmed here.
_MODE_HINTS: Final[tuple[tuple[re.Pattern[str], PermissionMode], ...]] = (
    (re.compile(r"bypass(?:ing)?\s+permissions", re.I), "bypassPermissions"),
    (re.compile(r"accept\s+edits\s+on", re.I), "acceptEdits"),
    (re.compile(r"plan\s+mode\s+on", re.I), "plan"),
    (re.compile(r"auto\s+mode\s+on", re.I), "auto"),
)


def strip_ansi(text: str) -> str:
    """One line with its SGR and OSC escapes removed.

    ``capture`` keeps escapes on purpose (colour is part of the terminal view),
    so every text decision here strips first — a pattern matched against a line
    carrying a colour run would miss it for no reason a reader could see.
    """
    return _ANSI.sub("", text)


def _bounded(value: str, limit: int) -> str:
    """``value`` cut to ``limit`` characters, with the cut made visible."""
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


@dataclass(frozen=True, slots=True)
class HookPayload:
    """One decoded hook payload: the nine keys, defensively read.

    ``cwd`` and ``transcript_path`` are present in the wire payload but not here
    as values — only as the booleans below — because both are filesystem paths
    and this record travels toward a browser.
    """

    hook: str
    session_id: str | None = None
    source: str | None = None
    model: str | None = None
    effort: str | None = None
    message: str | None = None
    prompt_chars: int | None = None
    stop_hook_active: bool = False
    transcript_present: bool = False
    cwd_present: bool = False


def _string(payload: Mapping[str, object], key: str) -> str | None:
    """A payload string, treating empty as absent — exactly ``cli/hook.py:_str``."""
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _effort_level(payload: Mapping[str, object]) -> str | None:
    """``effort.level``: an object whose ``level`` is a non-empty string, or nothing."""
    effort = payload.get("effort")
    if not isinstance(effort, Mapping):
        return None
    level = effort.get("level")
    return level if isinstance(level, str) and level else None


def decode_payload(hook: str, payload: object) -> HookPayload:
    """Decode a hook payload the way the hook commands already do, and no further.

    Non-JSON stdin, an empty body, a TTY and a JSON top level that is not an
    object all arrive here as something that is not a mapping, and all four
    produce the same empty payload rather than an error: a hook that raised on a
    malformed payload would be a hook that disrupts the agent.
    """
    data: Mapping[str, object] = payload if isinstance(payload, Mapping) else {}
    prompt = data.get("prompt")
    return HookPayload(
        hook=hook,
        session_id=_string(data, "session_id"),
        source=_string(data, "source"),
        model=_string(data, "model"),
        effort=_effort_level(data),
        message=_string(data, "message"),
        prompt_chars=len(prompt) if isinstance(prompt, str) else None,
        stop_hook_active=bool(data.get("stop_hook_active")),
        transcript_present=_string(data, "transcript_path") is not None,
        cwd_present=_string(data, "cwd") is not None,
    )


def hook_facts(
    agent_id: str, payload: HookPayload, *, observed_at: datetime
) -> tuple[HookFact, ...]:
    """Bounded, allow-listed facts from one hook delivery.

    Only names in :data:`HOOK_FACT_NAMES` are ever emitted, and a fact whose
    value is unknown is omitted rather than stored as an empty string — "the
    payload did not say" and "the payload said nothing" are different, and only
    the first is true here.
    """
    facts: list[HookFact] = [HookFact(agent_id, "hook", payload.hook, observed_at)]
    scalars: tuple[tuple[str, str | None], ...] = (
        ("source", payload.source),
        ("model", payload.model),
        ("effort", payload.effort),
        ("message", _bounded(payload.message, MAX_TEXT_CHARS) if payload.message else None),
        ("prompt_chars", str(payload.prompt_chars) if payload.prompt_chars is not None else None),
        ("transcript", "present" if payload.transcript_present else None),
    )
    facts.extend(
        HookFact(agent_id, name, value, observed_at) for name, value in scalars if value is not None
    )
    if payload.hook == "Stop":
        facts.append(
            HookFact(
                agent_id, "stop_hook_active", "1" if payload.stop_hook_active else "0", observed_at
            )
        )
    return tuple(facts)


@dataclass(frozen=True, slots=True)
class FrameDialog:
    """A dialog read off one frame: what it asks, and what it offers.

    ``selection`` is where the cursor is, which the frame shows and no hook does.
    ``raw`` is bounded pane text kept as evidence — never a transcript.
    """

    kind: QuestionKind
    text: str
    options: tuple[QuestionOption, ...] = ()
    selection: tuple[int, ...] | None = None
    header: str | None = None
    raw: str = ""

    def fingerprint(self) -> str:
        """A stable digest of what this dialog *is*, for lifecycle comparison.

        Deliberately excludes the selection: moving the cursor with an arrow key
        is not a new prompt, and a fingerprint that changed on it would mint a
        new lifecycle on every keystroke and invalidate an answer mid-choice.
        """
        parts = [self.kind, self.text, *(option.label for option in self.options)]
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class _Row:
    index: int
    ordinal: int
    label: str
    description: str | None
    selected: bool


def _rows(lines: Sequence[str]) -> list[_Row]:
    """Every line that renders as an option row, with its screen position."""
    found: list[_Row] = []
    for index, line in enumerate(lines):
        match = _OPTION.match(line)
        if match is None:
            continue
        rest = match.group("rest")
        # The mock separates a label from its dim description with two spaces;
        # after stripping colour that is the only separator left on screen.
        label, _, description = rest.partition("  ")
        found.append(
            _Row(
                index=index,
                ordinal=int(match.group("ordinal")),
                label=_bounded(label, MAX_LABEL_CHARS),
                description=_bounded(description, MAX_LABEL_CHARS) or None,
                selected=bool(match.group("marker")),
            )
        )
    return found


def _block(rows: Sequence[_Row]) -> list[_Row]:
    """The last run of consecutive rows numbered 1..n — the live option list.

    The *last* one because a pane's history may hold an answered dialog above the
    current screen, and the bottom-most complete list is the one being asked now.
    """
    best: list[_Row] = []
    current: list[_Row] = []
    for row in rows:
        expected = len(current) + 1
        contiguous = bool(current) and row.index == current[-1].index + 1
        if row.ordinal == expected and (contiguous or expected == 1):
            current.append(row)
        elif row.ordinal == 1:
            current = [row]
        else:
            current = []
        if len(current) >= 2 and len(current) <= MAX_OPTIONS:
            best = list(current)
    return best


def _text_above(lines: Sequence[str], index: int) -> str:
    """The nearest non-empty line above the option block: what is being asked."""
    for line in reversed(lines[:index]):
        if line.strip():
            return _bounded(line, MAX_TEXT_CHARS)
    return ""


def _header_above(lines: Sequence[str], index: int) -> str | None:
    """The ``[Tab] [Submit answers]`` strip above an ``AskUserQuestion``, if any."""
    for line in reversed(lines[:index]):
        if not line.strip():
            continue
        if _TAB_HEADER.match(line.strip()):
            return _bounded(line.strip().split("]")[0].lstrip("["), 12)
    return None


def _match_labels(
    rows: Sequence[_Row],
    table: Sequence[tuple[re.Pattern[str], str, OptionConsequence | None]],
) -> dict[int, tuple[str, OptionConsequence | None]]:
    """Rows whose label matches a known vocabulary, by row position."""
    matched: dict[int, tuple[str, OptionConsequence | None]] = {}
    for position, row in enumerate(rows):
        for pattern, key, consequence in table:
            if pattern.search(row.label):
                matched[position] = (key, consequence)
                break
    return matched


def classify_dialog(lines: Sequence[str]) -> FrameDialog | None:
    """The dialog this frame is showing, or ``None`` when it is not showing one.

    ``None`` is the common and correct answer: an agent at its prompt, mid-turn,
    or printing output has no dialog, and inventing one would put a row in the
    queue that nobody can answer. A list is never reduced to ``y``/``n`` — the
    keys and the on-screen order are the evidence, and P09 owns the keystrokes
    that act on them.
    """
    plain = [strip_ansi(line).rstrip() for line in lines[-MAX_SCREEN_LINES:]]
    block = _block(_rows(plain))
    if not block:
        return _fieldless(plain)

    first = block[0].index
    text = _text_above(plain, first)
    header = _header_above(plain, first)
    permission = _match_labels(block, _PERMISSION_LABELS)
    plan = _match_labels(block, _PLAN_LABELS)

    kind: QuestionKind
    known: dict[int, tuple[str, OptionConsequence | None]]
    if header is not None:
        kind, known = "ask", {}
    elif len(plan) >= 2:
        kind, known = "plan", plan
    elif len(permission) >= 2 and _PROCEED.search(text):
        kind, known = "permission", permission
    else:
        kind, known = "question", {}

    options: list[QuestionOption] = []
    for position, row in enumerate(block):
        key, consequence = known.get(position, (str(row.ordinal), None))
        options.append(
            QuestionOption(
                key=key,
                label=row.label,
                description=row.description,
                consequence=consequence,
            )
        )
    selection = tuple(position for position, row in enumerate(block) if row.selected)
    raw = _bounded(
        "\n".join(plain[first - 2 if first >= 2 else 0 : block[-1].index + 1]), MAX_RAW_CHARS
    )
    return FrameDialog(
        kind=kind,
        text=text,
        options=tuple(options),
        selection=selection or None,
        header=header,
        raw=raw,
    )


def _fieldless(lines: Sequence[str]) -> FrameDialog | None:
    """The two dialog shapes that carry no numbered list: a form, and a continue."""
    joined = "\n".join(lines)
    if _FIELD_MARKER in joined and _SUBMIT_HINT.search(joined):
        asked = next((line for line in lines if line.strip().endswith("asks:")), None)
        text = ""
        if asked is not None:
            after = lines[lines.index(asked) + 1 :]
            text = _text_below(after)
        return FrameDialog(
            kind="form",
            text=text or _bounded(lines[0] if lines else "", MAX_TEXT_CHARS),
            raw=_bounded(joined, MAX_RAW_CHARS),
        )
    for line in reversed(lines):
        if _CONTINUE_HINT.search(line):
            text = _bounded(line, MAX_TEXT_CHARS)
            return FrameDialog(
                kind="continue",
                text=text,
                options=(QuestionOption(key="continue", label=text),),
                raw=_bounded(joined, MAX_RAW_CHARS),
            )
    return None


def _text_below(lines: Sequence[str]) -> str:
    for line in lines:
        if line.strip():
            return _bounded(line, MAX_TEXT_CHARS)
    return ""


def permission_mode(lines: Sequence[str]) -> PermissionMode:
    """The permission mode the pane's own status line advertises.

    ``"unknown"`` is a real answer and the default: the hint is absent while the
    agent is mid-turn, and reporting ``"default"`` then would be a claim about
    the agent's configuration drawn from a frame that did not mention it.
    """
    for line in reversed([strip_ansi(line) for line in lines[-MAX_SCREEN_LINES:]]):
        for pattern, mode in _MODE_HINTS:
            if pattern.search(line):
                return mode
    return "unknown"


def mint_prompt_id(agent_id: str, generation: int, fingerprint: str) -> str:
    """An opaque, stable id for one observed prompt lifecycle.

    Internal evidence only. P09 owns the id a browser answers against and the
    fencing around it; this exists so the collector, the sidecar and the
    projector can agree on *which* observation they are each holding.
    """
    seed = f"{agent_id}\n{generation}\n{fingerprint}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class _Lifecycle:
    generation: int
    fingerprint: str | None


class PromptLifecycles:
    """Which observed prompt is which, across polls.

    The rule the contract needs: repeated identical text is a new lifecycle only
    when the pane cleared in between. So a fingerprint that is unchanged *and*
    uninterrupted keeps its generation — the same prompt, seen twice — while the
    same text after a clear gets a new one, and an answer prepared against the
    old generation is refused rather than landing on the new prompt.

    Held in memory by the collector rather than in the sidecar: it is a property
    of one server's continuous observation, and a restart that forgot it must
    mint *new* generations rather than resurrect old ones.
    """

    def __init__(self) -> None:
        self._state: dict[str, _Lifecycle] = {}

    def observe(self, agent_id: str, fingerprint: str | None) -> int | None:
        """The generation for what is on screen now, or ``None`` when nothing is.

        Calling with ``None`` is how the caller says "the pane was read and had
        no dialog" — which is what makes the *next* appearance a new lifecycle.
        """
        previous = self._state.get(agent_id)
        if fingerprint is None:
            if previous is not None and previous.fingerprint is not None:
                self._state[agent_id] = _Lifecycle(previous.generation, None)
            return None
        if previous is None:
            generation = 1
        elif previous.fingerprint == fingerprint:
            generation = previous.generation
        else:
            generation = previous.generation + 1
        self._state[agent_id] = _Lifecycle(generation, fingerprint)
        return generation

    def forget(self, agent_id: str) -> None:
        """Drop an agent that is gone, so its generations cannot be reused."""
        self._state.pop(agent_id, None)

    def generation_of(self, agent_id: str) -> int:
        """The last generation minted for ``agent_id``, or 0."""
        previous = self._state.get(agent_id)
        return previous.generation if previous is not None else 0


def frame_evidence(
    *,
    agent_id: str,
    dialog: FrameDialog,
    generation: int,
    observed_at: datetime,
    detected_by: DetectedBy = "frame",
    provider: str = PROVIDER,
) -> PromptEvidence:
    """Evidence for a dialog that was seen on screen.

    ``detected_by`` is ``frame`` unless a hook independently says the agent is in
    attention, in which case the caller passes ``both``. It is never ``hook`` for
    anything carrying options: no installed hook has ever seen one.
    """
    return PromptEvidence(
        agent_id=agent_id,
        provider=provider,
        prompt_id=mint_prompt_id(agent_id, generation, dialog.fingerprint()),
        generation=generation,
        kind=dialog.kind,
        detected_by=detected_by,
        observed_at=observed_at,
        options=dialog.options,
        selection=dialog.selection,
        raw=dialog.raw or None,
    )


def notification_evidence(
    *,
    agent_id: str,
    text: str,
    generation: int,
    observed_at: datetime,
    provider: str = PROVIDER,
    stale: bool = False,
) -> PromptEvidence:
    """Evidence that an agent is waiting, from the attention hook alone.

    The kind is ``question`` — the least-claiming member of the vocabulary —
    because ``Notification.message`` is opaque text the CLI never parses, and
    classifying a permission prompt from it would be inventing the one fact the
    payload provably does not carry. There are no options for the same reason.

    ``stale`` is the collector saying the pane was read and showed no dialog: the
    attention row outlived the prompt, which the board cannot notice on its own
    because ``mark_attention`` only fires on the transition *into* attention.
    """
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return PromptEvidence(
        agent_id=agent_id,
        provider=provider,
        prompt_id=mint_prompt_id(agent_id, generation, digest),
        generation=generation,
        kind="question",
        detected_by="hook",
        observed_at=observed_at,
        raw=_bounded(text, MAX_TEXT_CHARS) or None,
        stale=stale,
    )


__all__ = [
    "DEFAULT_NOTIFICATION",
    "HOOK_FACT_NAMES",
    "INSTALLED_HOOKS",
    "NOT_INSTALLED_HOOKS",
    "PAYLOAD_KEYS",
    "PROVIDER",
    "FrameDialog",
    "HookPayload",
    "PromptLifecycles",
    "classify_dialog",
    "decode_payload",
    "frame_evidence",
    "hook_facts",
    "mint_prompt_id",
    "notification_evidence",
    "permission_mode",
    "strip_ansi",
]
