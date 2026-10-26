"""The hand-off brief's bounds, its scrub and its files (review r22 of #113, #15's f3, f4, f10).

The brief is cut to fit the first prompt, and a cut that takes the END took what lets the agent
find the rest: with enough claims the prompt no longer named the brief file or told the agent to
read the board. It also carries the human's and the agent's words to ANOTHER agent's provider,
by default, so it is redacted as text leaving the machine is and the files it leaves are
pruned. Unit tests of ``services.handoff``; the end-to-end legs are in
``tests/test_hand_off_across_agents.py`` and ``tests/test_a_dialog_hand_off_crosses_agents.py``.
"""

from __future__ import annotations

import os
import stat
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aisquare.core.config import load_config, save_config
from aisquare.models import RedactionLevel, TeamEvent, TeamTask
from aisquare.services import handoff

KEY = "sk-ant-api03-" + "A1b2C3d4" * 6
PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    + "\n".join("MIIEowIBAAKCAQEA" + "x" * 48 for _ in range(4))
    + "\n-----END RSA PRIVATE KEY-----"
)
BRIEF_FILE = Path("/home/.aisquare/handoffs/brief.md")
KEPT = f"The whole brief is kept at {BRIEF_FILE}."


def _task(number: int, title: str = "Wire the auth flow", detail: str = "") -> TeamTask:
    now = datetime.now(tz=UTC)
    return TeamTask(
        id=f"tsk_{number}",
        project_id="prj",
        number=number,
        key=f"k{number}",
        title=title,
        detail=detail,
        status="doing",
        created_at=now,
        updated_at=now,
    )


def _event(n: int, text: str) -> TeamEvent:
    return TeamEvent(
        id=f"evt_{n}", project_id="prj", kind="note", text=text, created_at=datetime.now(tz=UTC)
    )


def _brief(
    claims: int = 0,
    *,
    changed: int = 0,
    conversation: tuple[str, ...] = (),
    redaction: RedactionLevel = RedactionLevel.standard,
    **more: object,
) -> handoff.HandoffBrief:
    """A brief at the real constants' worst: the contract past its cap, five entries past
    theirs, ``changed`` long uncommitted paths and ``claims`` claims of sixty characters."""
    base: dict[str, object] = {
        "label": "coder-1",
        "source": "coder-1",
        "source_agent": "codex",
        "target_agent": "claude-code",
        "reason": "it stopped on 5h limit",
        "task": _task(7, detail="objective: route every request. " * 100),
        "claims": tuple(_task(100 + n, f"claim number {n} " + "x" * 45) for n in range(claims)),
        "entries": tuple(_event(n, f"entry {n} " + "word " * 120) for n in range(5)),
        "git": handoff.GitState(
            tree=Path("/w"),
            branch="feat/x",
            changed=tuple(f" M src/pkg/{'deep/' * 12}file_{n}.py" for n in range(changed)),
            commits=("abc123 fix the router", "def456 add the router"),
        ),
        "conversation": conversation,
        "path": BRIEF_FILE,
        "redaction": redaction,
    }
    base.update(more)
    return handoff.HandoffBrief(**base)  # type: ignore[arg-type]


def _talk(count: int = 300) -> tuple[str, ...]:
    return tuple(f"line {n} " + "word " * 40 for n in range(count))


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


# --- f3: the cut keeps the head, and the last two lines ----------------------------------------


@pytest.mark.parametrize("claims", [0, 1, 8, 9, 20, 21, 40, 400])
def test_the_prompt_ends_with_where_to_start_and_where_the_brief_is_kept_at_any_claims(
    claims: int,
) -> None:
    """The review's measure: the contract at its cap, five entries at theirs, thirty paths and
    twenty claims dropped the brief file's path and the board line from the prompt."""
    text = _brief(claims, changed=30, conversation=_talk()).text()

    lines = text.split("\n")
    assert _size(text) <= handoff.PROMPT_MAX
    assert lines[-1] == KEPT
    assert lines[-2].startswith("Start by reading `aisquare board` and `aisquare task show tsk_7`")
    assert lines[-2].endswith("do not redo work that is already committed.")
    assert text.startswith(
        "You are coder-1, taking over from the previous session of this agent, which ran on Codex"
    )
    assert "Task (your task): Wire the auth flow (tsk_7)." in text


@pytest.mark.parametrize(
    ("claims", "listed", "more"), [(0, 0, None), (8, 8, None), (9, 8, 1), (40, 8, 32)]
)
def test_the_claims_are_counted_past_the_most_a_brief_lists(
    claims: int, listed: int, more: int | None
) -> None:
    text = _brief(claims).text()

    assert text.count("\n- #1") == listed  # one line per claim listed, the first #100
    if more is None:
        assert "more, on the board" not in text
    else:
        assert f"- (+{more} more, on the board)" in text
    if claims:
        assert "Claims, now yours:" in text


def test_a_claim_title_is_cut_like_the_other_lines_of_the_head() -> None:
    text = replace(_brief(0), claims=(_task(5, "T" * 5000),)).text()

    [claim] = [line for line in text.split("\n") if line.startswith("- #5 ")]
    assert claim == "- #5 " + "T" * handoff.CLAIM_MAX + " …"
    [task_line] = [line for line in text.split("\n") if line.startswith("Task (")]
    assert _size(task_line) < handoff.ENTRY_MAX + 60


def _present(text: str) -> tuple[bool, bool, bool, bool]:
    """Which of the head's parts a brief holds: the tree, the board entries, the claims, the
    task's contract."""
    return (
        "uncommitted:" in text,
        "last board entries:" in text,
        "Claims, now yours:" in text,
        "Contract:" in text,
    )


def test_a_head_over_the_limit_gives_way_entries_first_then_the_tree_then_the_claims() -> None:
    """Whatever the limit: the opening, the task and the last two lines stay, and a part goes
    only after every part that gives way before it: the entries, the tree, the claims, the
    contract. The conversation is a pointer by then."""
    brief = _brief(8, changed=30, conversation=_talk())
    whole = brief.text(handoff.FILE_MAX)
    assert _present(whole) == (True, True, True, True) and "line 299" in whole

    held: dict[tuple[bool, bool, bool, bool], int] = {}
    for limit in range(600, 9_000, 100):
        text = brief.text(limit)
        lines = text.split("\n")
        assert _size(text) <= limit, limit
        assert lines[-1] == KEPT and lines[-2].startswith("Start by reading `aisquare board`")
        tree, entries, claims, contract = held_now = _present(text)
        assert text.startswith("You are coder-1, taking over from the previous session")
        assert "Task (your task): Wire the auth flow (tsk_7)." in text or limit < 900, limit
        # the entries go before the tree, the tree before the claims, the claims before the
        # contract: a part that is still here has every later one with it
        assert not entries or tree, limit
        assert not tree or claims, limit
        assert not claims or contract, limit
        held[held_now] = limit
    assert (True, True, True, True) in held and (False, False, False, False) in held
    assert (False, False, True, True) in held and (False, False, False, True) in held


def test_the_conversation_keeps_its_newest_lines_when_the_head_leaves_room() -> None:
    text = _brief(1, conversation=_talk()).text(handoff.FILE_MAX // 4)

    assert "line 299" in text and "line 0 " not in text and "its start cut to fit" in text
    assert text.endswith(KEPT)


def test_the_board_entries_go_one_at_a_time_the_oldest_first() -> None:
    brief = _brief(0, changed=0, conversation=())
    shown: set[frozenset[int]] = set()

    for limit in range(2_000, 6_000, 25):
        text = brief.text(limit)
        kept = frozenset(n for n in range(5) if f"- note: entry {n} " in text)
        assert kept == frozenset(range(5 - len(kept), 5)), (limit, kept)  # a newest run
        shown.add(kept)
    assert frozenset() in shown and frozenset(range(5)) in shown
    assert any(0 < len(kept) < 5 for kept in shown)


def test_what_the_cut_took_is_said_and_where_to_read_it() -> None:
    text = _brief(8, changed=30, conversation=_talk()).text(4_000)

    assert "(cut to fit: the board entries, the tree; the board and the tree have them)" in text
    assert "uncommitted:" not in text and "last board entries:" not in text


def test_a_head_that_fits_is_cut_nowhere_and_says_nothing_of_a_cut() -> None:
    text = _brief(2, changed=2, conversation=("> hi", "  hello")).text()

    assert "cut to fit" not in text
    assert _present(text) == (True, True, True, True)
    assert "> hi" in text and "  hello" in text


def test_a_head_over_the_limit_points_at_no_file_when_none_was_kept() -> None:
    text = _brief(8, changed=30, conversation=_talk(), path=None).text(900)

    assert "(coder-1's conversation is too long for here)" in text and "brief file" not in text


def test_a_head_over_the_limit_points_at_the_file_when_one_was_kept() -> None:
    text = _brief(8, changed=30, conversation=_talk()).text(900)

    assert "conversation is in the brief file, too long for here" in text


def test_the_whole_brief_keeps_all_the_conversation_the_prompt_cuts() -> None:
    brief = _brief(2, conversation=_talk(200))

    prompt, whole = brief.text(), brief.text(handoff.FILE_MAX)

    assert "its start cut to fit" in prompt and "line 0 " not in prompt
    assert "its start cut to fit" not in whole and "line 0 " in whole
    assert _size(whole) <= handoff.FILE_MAX


def test_a_limit_below_the_last_two_lines_still_bounds_the_brief() -> None:
    """The final guard: nothing a caller asks for makes the brief longer than the limit."""
    for limit in (400, 120, 20, 0):
        assert _size(_brief(40, changed=30, conversation=_talk()).text(limit)) <= limit


def test_a_cut_never_splits_a_character() -> None:
    brief = _brief(0, conversation=("é" * 3000,), changed=0)

    text = brief.text(900)

    assert _size(text) <= 900
    assert "�" not in text and text.encode("utf-8").decode("utf-8") == text


# --- f10: a take-over is never "from" its own label ---------------------------------------------


def test_a_take_over_opens_as_the_previous_session_of_the_same_agent() -> None:
    text = _brief(0, label="coder-1", source="coder-1").text()

    assert "taking over from the previous session of this agent, which ran on Codex" in text
    assert "taking over from coder-1" not in text


def test_a_take_over_by_another_label_names_the_agent_it_takes_over_from() -> None:
    text = _brief(0, label="coder-2", source="coder-1").text()

    assert text.startswith("You are coder-2, taking over from coder-1, which ran on Codex")


def test_a_fork_names_the_agent_it_forks() -> None:
    text = _brief(0, label="coder-2", source="coder-1", fork=True).text()

    assert text.startswith("You are coder-2, a fork of coder-1, which keeps running on Codex")
    assert "previous session" not in text


# --- f4: the brief is redacted before it is written or sent -----------------------------------


def test_a_key_in_the_conversation_does_not_reach_the_prompt_or_the_file() -> None:
    conversation = (
        "> use this to log in",
        f"  ANTHROPIC_API_KEY={KEY}",
        "  and my token is ghp_" + "a1B2c3D4" * 5,
    )
    brief = handoff.write(_brief(1, conversation=conversation, path=None))
    assert brief.path is not None

    for text in (handoff.prompt(brief), brief.path.read_text(encoding="utf-8")):
        assert KEY not in text and "ghp_a1B2" not in text, text
        assert "ANTHROPIC_API_KEY=[redacted]" in text
        assert "use this to log in" in text  # the words around it are kept


def test_a_key_that_spans_lines_is_redacted_as_one() -> None:
    """The transcript wraps a pasted PEM block over lines; it is scrubbed as one text."""
    conversation = ("> here is the key", *PEM.split("\n"), "> that is all")

    text = _brief(0, conversation=conversation).text()

    assert "MIIEowIBAAKCAQEA" not in text and "BEGIN RSA PRIVATE KEY" not in text
    assert "[redacted]" in text and "that is all" in text


def test_a_key_in_the_board_text_is_redacted_too() -> None:
    brief = _brief(
        0,
        task=_task(7, "Rotate the key", detail=f"the old key is API_KEY={KEY} and is rotated"),
        entries=(_event(1, f"pasted Authorization: Bearer {KEY}"),),
    )

    text = brief.text()

    assert KEY not in text and "the old key is API_KEY=[redacted]" in text
    assert "- note: pasted Authorization: Bearer [redacted]" in text


def test_the_last_two_lines_are_ours_and_are_not_scrubbed() -> None:
    """The brief file's path is the agent's way to the rest, whatever the level."""
    brief = _brief(0, redaction=RedactionLevel.strict, path=Path("/Users/someone/h/b.md"))

    assert brief.text().endswith("The whole brief is kept at /Users/someone/h/b.md.")


def test_strict_takes_who_and_where_out_as_well_and_off_sends_as_typed() -> None:
    conversation = ("> mail me@example.com about /Users/anmol/Work/x", f"  KEY={KEY}")
    standard = _brief(0, conversation=conversation).text()
    strict = _brief(0, conversation=conversation, redaction=RedactionLevel.strict).text()
    off = _brief(0, conversation=conversation, redaction=RedactionLevel.off).text()

    assert "me@example.com" in standard and f"KEY={KEY}" not in standard
    assert "me@example.com" not in strict and "/Users/anmol" not in strict and KEY not in strict
    assert "[redacted] about ~/Work/x" in strict
    assert f"KEY={KEY}" in off


def test_a_brief_is_made_at_the_level_the_config_holds(tmp_path: Path) -> None:
    config = load_config()
    config.redaction.level = RedactionLevel.strict
    save_config(config)
    assert handoff._redaction_level() is RedactionLevel.strict

    config.redaction.level = RedactionLevel.off
    save_config(config)
    assert handoff._redaction_level() is RedactionLevel.off


def test_a_config_that_cannot_be_read_scrubs_at_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken() -> object:
        raise ValueError("broken config")

    monkeypatch.setattr(handoff, "load_config", broken)

    assert handoff._redaction_level() is RedactionLevel.standard


def test_a_brief_made_by_hand_is_scrubbed_at_the_default() -> None:
    assert _brief().redaction is RedactionLevel.standard
    assert (
        handoff.HandoffBrief(
            label=None, source="a", source_agent="codex", target_agent="claude-code"
        ).redaction
        is RedactionLevel.standard
    )


# --- f4: the files are pruned ---------------------------------------------------------------


def _aged(path: Path, days: float, *, now: datetime) -> Path:
    path.write_text("a brief\n", encoding="utf-8")
    stamp = (now - timedelta(days=days)).timestamp()
    os.utime(path, (stamp, stamp), follow_symlinks=False)
    return path


def test_the_briefs_older_than_the_days_kept_are_removed_when_one_is_written() -> None:
    now = datetime.now(tz=UTC)
    folder = handoff.brief_dir()
    folder.mkdir(parents=True, exist_ok=True)
    old = _aged(folder / "20250101T000000Z-coder-1-aaaaaaaa.md", handoff.KEEP_DAYS + 1, now=now)
    edge = _aged(folder / "20250102T000000Z-coder-1-bbbbbbbb.md", handoff.KEEP_DAYS - 1, now=now)
    fresh = _aged(folder / "20250103T000000Z-coder-1-cccccccc.md", 0, now=now)

    written = handoff.write(_brief(0), now=now)

    assert written.path is not None and written.path.is_file()
    assert not old.exists()
    assert edge.exists() and fresh.exists()


def test_a_brief_is_kept_for_two_weeks() -> None:
    assert handoff.KEEP_DAYS == 14


def test_prune_says_which_it_removed_and_leaves_what_is_not_a_brief() -> None:
    now = datetime.now(tz=UTC)
    folder = handoff.brief_dir()
    folder.mkdir(parents=True, exist_ok=True)
    old = _aged(folder / "old.md", 30, now=now)
    other = _aged(folder / "notes.txt", 30, now=now)
    sub = folder / "sub.md"
    sub.mkdir()
    spared = _aged(folder / "spared.md", 30, now=now)
    elsewhere = _aged(folder.parent / "elsewhere.md", 30, now=now)

    removed = handoff.prune(now=now, spare=spared)

    assert removed == (old,)
    assert other.exists() and sub.is_dir() and spared.exists() and elsewhere.exists()
    assert handoff.prune(now=now, spare=spared) == ()


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_prune_removes_no_link_and_nothing_a_link_points_at(tmp_path: Path) -> None:
    now = datetime.now(tz=UTC)
    folder = handoff.brief_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = _aged(tmp_path / "precious.md", 30, now=now)
    link = folder / "link.md"
    link.symlink_to(target)
    os.utime(link, ((now - timedelta(days=30)).timestamp(),) * 2, follow_symlinks=False)

    assert handoff.prune(now=now) == ()
    assert link.is_symlink() and target.exists()


def test_prune_of_a_folder_that_is_not_there_removes_nothing() -> None:
    assert not handoff.brief_dir().exists()

    assert handoff.prune() == ()


def test_a_file_that_will_not_go_is_left_for_the_next_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(tz=UTC)
    folder = handoff.brief_dir()
    folder.mkdir(parents=True, exist_ok=True)
    stuck = _aged(folder / "stuck.md", 30, now=now)
    gone = _aged(folder / "z-gone.md", 30, now=now)

    def refuse(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == "stuck.md":
            raise PermissionError("no")
        os.unlink(self)

    monkeypatch.setattr(Path, "unlink", refuse)

    assert handoff.prune(now=now) == (gone,)
    assert stuck.exists()


def test_the_brief_file_is_written_even_when_the_prune_finds_nothing_to_do() -> None:
    written = handoff.write(_brief(0))

    assert written.path is not None
    assert stat.S_IMODE(written.path.stat().st_mode) == 0o600 or os.name == "nt"
    assert written.path.read_text(encoding="utf-8").endswith(f"{written.path}.\n")


# --- f4, tester's round: a secret is scrubbed whole, before it is wrapped, windowed or clipped ---

AKIA = "AKIAIOSFODNN7EXAMPLE"
RSA = [
    "-----BEGIN RSA PRIVATE KEY-----",
    *(f"MIIEowIBAAKCAQEA{n:02d}" + "q9Zx" * 15 for n in range(26)),
    "-----END RSA PRIVATE KEY-----",
]


def _bare() -> handoff.HandoffBrief:
    """A brief of a conversation alone, so that nearly all of the first prompt is its window."""
    return handoff.HandoffBrief(
        label="coder-1",
        source="coder-1",
        source_agent="codex",
        target_agent="claude-code",
        path=BRIEF_FILE,
    )


def _later(count: int) -> tuple[str, ...]:
    return tuple(f"later {n} " + "word " * 30 for n in range(count))


def test_a_key_the_window_cuts_the_start_of_is_gone_with_the_rest() -> None:
    """The tester's (c): a PEM block pasted, then enough said that the first prompt's window
    starts inside it. BEGIN is outside the window, so the PEM rule could not match what was
    left; the whole conversation is scrubbed first. Swept over how much was said after it, so
    the window's start falls on every line of the block."""
    for limit, counts in (
        (handoff.PROMPT_MAX, range(1, 90)),
        (handoff.FILE_MAX, range(480, 800, 7)),
    ):
        for count in counts:
            text = replace(_bare(), conversation=(*RSA, *_later(count))).text(limit)

            assert "q9Zx" not in text and "MIIEowIBAAKCAQEA" not in text, (limit, count)
            assert f"later {count - 1} " in text, "the newest of the conversation is there"
            assert text.endswith(KEPT)


def test_a_key_over_two_hundred_characters_in_one_line_is_scrubbed_before_it_is_clipped() -> None:
    """The tester's clip case: a line cut at 400 left 'AKIAIOSFODNN7EXAMPL', one character short
    of a pattern, in the brief."""
    line = "x" * (handoff.ENTRY_MAX - 5) + " " + AKIA + " and more words after it"
    text = _brief(0, conversation=(line,)).text()

    assert AKIA[:12] not in text
    assert "xxxxxxxx" in text


@pytest.mark.parametrize(
    ("where", "bound"),
    [("title", handoff.ENTRY_MAX), ("detail", handoff.DETAIL_MAX)],
)
def test_a_key_on_the_edge_of_a_clip_in_the_task_is_scrubbed_first(where: str, bound: int) -> None:
    made = "x" * (bound - 11) + " " + AKIA + " tail"
    task = _task(7, **({"title": made} if where == "title" else {"detail": made}))

    text = replace(_brief(0), task=task).text()

    assert AKIA[:10] not in text, where


def test_a_key_on_the_edge_of_a_clip_in_an_entry_or_a_claim_is_scrubbed_first() -> None:
    entry = _event(1, "y" * (handoff.ENTRY_MAX - 11) + " " + AKIA + " tail")
    claim = _task(9, "z" * (handoff.CLAIM_MAX - 11) + " " + AKIA + " tail")

    text = replace(_brief(0), entries=(entry,), claims=(claim,)).text()

    assert AKIA[:10] not in text


def test_the_head_is_scrubbed_at_the_level_the_brief_holds() -> None:
    """The task, the claims, the board entries and the tree: strict takes who and where out of
    them as it does from the conversation; off sends them as typed (tester's pin gap)."""
    detail = f"mail me@example.com about /Users/anmol/Work/x with API_KEY={KEY}"
    brief = replace(
        _brief(0),
        task=_task(7, "Fix /Users/anmol/Work/y", detail=detail),
        claims=(_task(9, "ask me@example.com"),),
        entries=(_event(1, "from /Users/anmol/Work/z"),),
        git=handoff.GitState(
            tree=Path("/Users/anmol/Work/proj"),
            branch="fix/me@example.com",
            changed=(" M /Users/anmol/Work/a.py",),
            commits=("abc1234 thanks me@example.com",),
        ),
    )
    strict = replace(brief, redaction=RedactionLevel.strict).text()
    standard = replace(brief, redaction=RedactionLevel.standard).text()
    off = replace(brief, redaction=RedactionLevel.off).text()

    for gone in ("me@example.com", "/Users/anmol", KEY):
        assert gone not in strict, gone
    assert "mail [redacted] about ~/Work/x" in strict and "Fix ~/Work/y" in strict
    assert "me@example.com" in standard and "/Users/anmol/Work/x" in standard
    assert KEY not in standard and "API_KEY=[redacted]" in standard
    assert f"API_KEY={KEY}" in off and "/Users/anmol/Work/z" in off


@pytest.mark.parametrize("fork", [False, True])
def test_a_label_of_two_hundred_characters_keeps_the_last_two_lines(fork: bool) -> None:
    long = "coder-" + "x" * 200
    brief = _brief(8, changed=30, conversation=_talk(), label=long, source=long, fork=fork)

    text = brief.text()
    lines = text.split("\n")

    assert _size(text) <= handoff.PROMPT_MAX
    assert lines[-1] == KEPT and lines[-2].startswith("Start by reading `aisquare board`")
    assert long not in text and "x" * 200 not in text
    assert "coder-" + "x" * (handoff.LABEL_MAX - len("coder-")) + " …" in text, "cut, said so"
    assert "x" * handoff.LABEL_MAX not in text


def test_scrub_entry_redacts_every_string_of_a_record_and_nothing_else() -> None:
    record = {
        "type": "user",
        "n": 3,
        "ok": True,
        "message": {"content": [{"text": f"KEY={KEY}"}, {"text": "plain words"}, 4, None]},
    }
    scrubbed = handoff.scrub_entry(record, RedactionLevel.standard)

    assert scrubbed == {
        "type": "user",
        "n": 3,
        "ok": True,
        "message": {"content": [{"text": "KEY=[redacted]"}, {"text": "plain words"}, 4, None]},
    }
    assert record["message"]["content"][0]["text"] == f"KEY={KEY}", "the record read is not changed"  # type: ignore[index]
    assert handoff.scrub_entry(record, RedactionLevel.off) is record
    deep: object = "x"
    for _ in range(80):
        deep = [deep]
    flat = handoff.scrub_entry(deep, RedactionLevel.standard)
    while isinstance(flat, list):
        flat = flat[0]
    assert flat == "[redacted]", "a structure no transcript has is replaced, not walked"
