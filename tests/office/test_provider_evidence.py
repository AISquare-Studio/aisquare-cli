"""What this back-end may claim it observes, and what it actually reads off a frame.

The dialog fixtures below are the *contract's own* rendering of a prompt — the
shapes ``tools/office-mock-server.mjs`` builds for v1.3 ("a prompt is described
by what the pane shows") and the option vocabularies frozen into the 1.4
fixtures. They are not captured from a live terminal, and that limit is real:
every Claude pane on this machine runs its TUI on tmux's alternate screen
(``alternate_on=1``, ``history_size=0`` on all five live panes), so there is no
scrollback to mine and no dialog was on screen to capture. ``LIVE_IDLE_FRAME`` is
the one fixture taken from a real pane, and it is the negative case: chrome, a
prompt line and a mode hint, with no dialog anywhere in it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from aisquare.core import agents
from aisquare.office.providers import (
    HOOK_CAPABLE_PROVIDERS,
    HOOKS_NOT_INSTALLED_REASON,
    KNOWN_PROVIDERS,
    NO_HOOK_PATH_REASON,
    claude_observe,
    provider_capabilities,
)

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RESET = "\x1b[0m"
CURSOR = "\u276f"
"""The pane's selection marker, escaped rather than pasted so it stays legible."""

PERMISSION_FRAME = (
    f"{BOLD}Bash command{RESET}",
    "",
    "make check",
    "",
    "Do you want to proceed?",
    f"{CURSOR} 1. Yes",
    "  2. Yes, and don't ask again for similar commands in ~/code/aisquare-cli",
    "  3. No, and tell Claude what to do differently (esc)",
)

EDIT_PERMISSION_FRAME = (
    f"{BOLD}Edit file{RESET}",
    "",
    "src/aisquare/cli/office.py",
    "",
    "Do you want to proceed?",
    f"{CURSOR} 1. Yes",
    "  2. Yes, allow all edits during this session",
    "  3. No, and tell Claude what to do differently (esc)",
)

PLAN_FRAME = (
    f"{BOLD}Ready to code?{RESET}",
    "",
    "Here is Claude's plan:",
    "# Plan",
    "1. Read",
    "",
    "Would you like to proceed?",
    f"{CURSOR} 1. Yes, auto-accept edits",
    "  2. Yes, manually approve edits",
    "  3. No, keep planning",
)

ASK_FRAME = (
    f"{BOLD}[Fallback]{RESET} {DIM}[Submit answers]{RESET}",
    "",
    "The 3DS challenge can time out. Which fallback do you want?",
    f"{CURSOR} 1. Retry through legacy /v1/charges  {DIM}one more attempt on the old path{RESET}",
    f"  2. Fail closed, open a manual-review task  {DIM}no charge; a reviewer decides{RESET}",
    f"  3. Other  {DIM}type your own answer{RESET}",
)

FORM_FRAME = (
    f"{BOLD}mcp__svc_notify__receipt_lookup{RESET} asks:",
    "svc-notify needs details to continue: receipt id and environment",
    "",
    "  receipt id: ▁▁▁▁▁▁▁▁",
    "  environment: ▁▁▁▁▁▁▁▁",
    "",
    f"{DIM}Tab between fields · Enter to submit · Esc to cancel{RESET}",
)

CONTINUE_FRAME = ("\x1b[33mPress Enter to continue\x1b[0m",)

#: Captured from a live fleet pane on 2026-09-11 (chrome only — no teammate
#: content). The agent is at its prompt with no dialog, which is what almost
#: every frame looks like and therefore the case a classifier must not guess at.
LIVE_IDLE_FRAME = (
    "● Ran 3 shell commands",
    "",
    "──────────────────────────────────────────── coder-observe ─",
    f"{CURSOR} ",
    "────────────────────────────────────────────────────────────",
    "  [CAVEMAN]",
    "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents",
)


def test_capabilities_report_claude_with_hooks_and_the_others_pane_only() -> None:
    """The matrix is per provider, and only one provider has a hook path."""
    rows = {row.id: row for row in provider_capabilities(probe=lambda _name: True)}

    assert sorted(rows) == sorted(KNOWN_PROVIDERS)
    assert rows["claude-code"].observation == "hooks_and_pane"
    assert rows["codex"].observation == "pane_only"
    assert rows["cursor"].observation == "pane_only"
    assert rows["codex"].reason == NO_HOOK_PATH_REASON
    assert rows["cursor"].reason == NO_HOOK_PATH_REASON


def test_no_provider_claims_structured_input_at_this_revision() -> None:
    """No installed hook has ever carried an option, so nobody may claim one."""
    rows = provider_capabilities(probe=lambda _name: True)

    assert [row.structured_input for row in rows] == [False, False, False]


def test_claude_degrades_to_pane_only_when_its_hooks_are_not_installed() -> None:
    """An uninstalled hook produces exactly as much evidence as an absent one."""
    rows = {row.id: row for row in provider_capabilities(probe=lambda _name: False)}

    assert rows["claude-code"].observation == "pane_only"
    assert rows["claude-code"].reason == HOOKS_NOT_INSTALLED_REASON


def test_the_hook_allow_list_matches_what_install_hooks_actually_writes(tmp_path: Path) -> None:
    """The five are read off a real install, not copied from a plan.

    This is the assertion that would have caught the packet's own error: its
    plan names eight further hook kinds, and installing for real writes none of
    them.
    """
    config_dir = tmp_path / "claude"
    assert agents.install_hooks("claude-code", config_dir) is True

    settings = json.loads((config_dir / "settings.json").read_text(encoding="utf-8"))
    installed = set(settings["hooks"])

    assert installed == set(claude_observe.INSTALLED_HOOKS)
    assert installed.isdisjoint(claude_observe.NOT_INSTALLED_HOOKS)


def test_providers_without_a_settings_path_cannot_install_hooks(tmp_path: Path) -> None:
    """``HOOK_CAPABLE_PROVIDERS`` is a fact about ``AgentSpec``, checked against it."""
    for provider in KNOWN_PROVIDERS:
        installed = agents.install_hooks(provider, tmp_path / provider)
        assert installed is (provider in HOOK_CAPABLE_PROVIDERS)


def test_a_permission_prompt_keeps_its_labels_keys_order_and_selection() -> None:
    """Option keys and on-screen order are evidence; the cursor is the selection."""
    dialog = claude_observe.classify_dialog(PERMISSION_FRAME)

    assert dialog is not None
    assert dialog.kind == "permission"
    assert dialog.text == "Do you want to proceed?"
    assert [option.key for option in dialog.options] == ["allow", "allow-remember", "deny"]
    assert dialog.options[1].label.startswith("Yes, and don't ask again for similar commands")
    assert dialog.options[1].consequence == "remembers"
    assert dialog.selection == (0,)


def test_an_edit_permission_remembers_only_for_the_session() -> None:
    """The second option's consequence differs by tool, and is read, not assumed."""
    dialog = claude_observe.classify_dialog(EDIT_PERMISSION_FRAME)

    assert dialog is not None
    assert dialog.options[1].consequence == "session"


def test_a_plan_prompt_is_classified_by_its_own_option_vocabulary() -> None:
    dialog = claude_observe.classify_dialog(PLAN_FRAME)

    assert dialog is not None
    assert dialog.kind == "plan"
    assert [option.key for option in dialog.options] == ["approve", "approve-manual", "revise"]
    assert dialog.options[0].consequence == "mode_accept_edits"


def test_an_ask_keeps_ordinal_keys_because_the_frame_shows_no_others() -> None:
    """A frame offers a numbered list; ``opt1``/``other`` live in a hook this CLI
    does not install, so inventing them here would be inventing the evidence."""
    dialog = claude_observe.classify_dialog(ASK_FRAME)

    assert dialog is not None
    assert dialog.kind == "ask"
    assert [option.key for option in dialog.options] == ["1", "2", "3"]
    assert dialog.options[0].label == "Retry through legacy /v1/charges"
    assert dialog.options[0].description == "one more attempt on the old path"
    assert dialog.header == "Fallback"


def test_a_form_and_a_continue_are_recognised_without_a_numbered_list() -> None:
    form = claude_observe.classify_dialog(FORM_FRAME)
    keep_going = claude_observe.classify_dialog(CONTINUE_FRAME)

    assert form is not None and form.kind == "form"
    assert form.text.startswith("svc-notify needs details")
    assert keep_going is not None and keep_going.kind == "continue"
    assert [option.key for option in keep_going.options] == ["continue"]


def test_a_live_pane_with_no_dialog_classifies_as_nothing() -> None:
    """The captured negative case. Guessing here puts an unanswerable row in the queue."""
    assert claude_observe.classify_dialog(LIVE_IDLE_FRAME) is None


def test_a_list_is_never_reduced_to_yes_or_no() -> None:
    """Every option survives with its own key; three on screen is never two."""
    dialog = claude_observe.classify_dialog(PERMISSION_FRAME)

    assert dialog is not None
    assert len(dialog.options) == 3
    assert {option.key for option in dialog.options} != {"allow", "deny"}


def test_the_permission_mode_hint_is_read_off_a_real_frame() -> None:
    """``auto mode on`` is the one hint measured against a live pane."""
    assert claude_observe.permission_mode(LIVE_IDLE_FRAME) == "auto"
    assert claude_observe.permission_mode(PERMISSION_FRAME) == "unknown"


def test_a_malformed_payload_decodes_to_nothing_rather_than_raising() -> None:
    """Every shape ``cli/hook.py`` degrades on is handled here the same way."""
    payloads: tuple[object, ...] = (None, [], "", 7, {"prompt": 5}, {"session_id": ""})
    for payload in payloads:
        decoded = claude_observe.decode_payload("Stop", payload)
        assert decoded.session_id is None
        assert decoded.hook == "Stop"


def test_hook_facts_record_the_prompt_length_and_never_the_prompt_or_a_path() -> None:
    """The prompt is already an egress surface; Office must not widen it."""
    payload = claude_observe.decode_payload(
        "UserPromptSubmit",
        {
            "prompt": "secret",
            "transcript_path": "/home/someone/.claude/projects/x.jsonl",
            "cwd": "/home/someone/code",
            "model": "claude-opus-5",
            "effort": {"level": "high"},
        },
    )

    facts = claude_observe.hook_facts("agt-1", payload, observed_at=NOW)
    values = {fact.name: fact.value for fact in facts}

    assert values["prompt_chars"] == "6"
    assert values["transcript"] == "present"
    assert values["model"] == "claude-opus-5"
    assert values["effort"] == "high"
    assert "secret" not in str(values)
    assert ".jsonl" not in str(values)
    assert "/home/someone" not in str(values)
    assert set(values).issubset(claude_observe.HOOK_FACT_NAMES)


def test_the_same_prompt_seen_twice_keeps_one_generation() -> None:
    """Polling is not a new lifecycle, or an answer would go stale mid-choice."""
    lifecycles = claude_observe.PromptLifecycles()
    dialog = claude_observe.classify_dialog(PERMISSION_FRAME)
    assert dialog is not None

    first = lifecycles.observe("agt-1", dialog.fingerprint())
    second = lifecycles.observe("agt-1", dialog.fingerprint())

    assert first == second == 1


def test_the_same_text_after_the_pane_cleared_is_a_new_lifecycle() -> None:
    """SHARED.md §6: repeat text with a new lifecycle gets a different id."""
    lifecycles = claude_observe.PromptLifecycles()
    dialog = claude_observe.classify_dialog(PERMISSION_FRAME)
    assert dialog is not None

    first = lifecycles.observe("agt-1", dialog.fingerprint())
    lifecycles.observe("agt-1", None)
    second = lifecycles.observe("agt-1", dialog.fingerprint())

    assert first == 1
    assert second == 2
    assert claude_observe.mint_prompt_id("agt-1", 1, dialog.fingerprint()) != (
        claude_observe.mint_prompt_id("agt-1", 2, dialog.fingerprint())
    )


def test_moving_the_cursor_is_not_a_new_prompt() -> None:
    """The fingerprint excludes the selection, or every arrow key would re-mint."""
    first = claude_observe.classify_dialog(PERMISSION_FRAME)
    moved = claude_observe.classify_dialog(
        (
            *PERMISSION_FRAME[:5],
            "  1. Yes",
            f"{CURSOR} 2. Yes, and don't ask again for similar commands in ~/code/aisquare-cli",
            "  3. No, and tell Claude what to do differently (esc)",
        )
    )

    assert first is not None and moved is not None
    assert first.fingerprint() == moved.fingerprint()
    assert moved.selection == (1,)


def test_notification_evidence_claims_no_kind_and_no_options() -> None:
    """``Notification.message`` is opaque text; classifying it would invent the fact."""
    evidence = claude_observe.notification_evidence(
        agent_id="ses-1", text="Claude is waiting for your input", generation=1, observed_at=NOW
    )

    assert evidence.kind == "question"
    assert evidence.detected_by == "hook"
    assert evidence.options == ()
    assert evidence.stale is False


def test_frame_evidence_is_never_detected_by_hook_alone() -> None:
    """Options exist only on screen, so anything carrying them says so."""
    dialog = claude_observe.classify_dialog(PERMISSION_FRAME)
    assert dialog is not None

    evidence = claude_observe.frame_evidence(
        agent_id="agt-1", dialog=dialog, generation=1, observed_at=NOW
    )
    both = claude_observe.frame_evidence(
        agent_id="agt-1", dialog=dialog, generation=1, observed_at=NOW, detected_by="both"
    )

    assert evidence.detected_by == "frame"
    assert both.detected_by == "both"
    assert both.options == dialog.options


def test_evidence_is_bounded_before_it_is_stored() -> None:
    """A pane can emit a megabyte-long line; evidence is not a transcript."""
    dialog = claude_observe.classify_dialog(
        (
            "Do you want to proceed?",
            f"{CURSOR} 1. " + "y" * 5_000,
            "  2. No, and tell Claude what to do differently (esc)",
        )
    )

    assert dialog is not None
    assert len(dialog.options[0].label) <= claude_observe.MAX_LABEL_CHARS
    assert len(dialog.raw) <= claude_observe.MAX_RAW_CHARS
