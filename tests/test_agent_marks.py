"""Each agent's mark where one table or list holds more than one agent's accounts (R16-C).

The owner, at a machine with Claude Code and Codex accounts (2026-11-13): "Cl Co is shown in
account listing but since both start with C it is hard to tell apart, can we maybe use their
icons?" A terminal has no logos: Unicode has none for either, and a Nerd Font glyph is only
as good as the viewer's font (the owner reads asq over SSH from a Windows terminal too). So
each agent's adapter declares the mark its own TUI shows (``AgentAdapter.mark``): Claude
Code's ``✻``, the glyph of its welcome banner and spinner, in its orange; Codex's ``>_``, the
mark of its header, in blue. Shared code spells neither (``tests/test_no_agent_literals.py``'s
``mark`` shape), and an agent that declares none keeps the letters every agent had.

Pinned here: the marks and their colours, that every glyph is one cell wide wherever it is
drawn (no East-Asian-ambiguous or wide character, by Rich's ``cell_len`` too), that each
mark's dark and light colours are one hue, the padding that keeps the
names after the marks in one column, and the account picker's rows. The Accounts page itself,
NO_COLOR and a Claude-only machine's unchanged screens are pinned in ``tests/test_ui_accounts.py``.
"""

from __future__ import annotations

import colorsys
import unicodedata
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from rich.cells import cell_len
from textual.color import Color

from aisquare.cli.ui.attach import (
    AttachTargetScreen,
    Targets,
    account_prompt,
    account_text,
    accounts_title,
)
from aisquare.cli.ui.views import accounts as accounts_view
from aisquare.core import claude_accounts as accounts_core
from aisquare.core.agent_adapters import adapters, register, unregister
from aisquare.core.agent_adapters.types import AgentAdapter, AgentMark
from aisquare.models import ClaudeAccount, ClaudeAccountStatus, ClaudeIdentity
from tests.fake_agent import FakeAgentAdapter

MARKED: tuple[AgentAdapter, ...] = tuple(adapter for adapter in adapters() if adapter.mark)


def _mark(adapter: AgentAdapter) -> AgentMark:
    assert adapter.mark is not None
    return adapter.mark


def test_claude_code_and_codex_carry_the_marks_their_own_tuis_show() -> None:
    assert {adapter.id: adapter.mark for adapter in adapters()} == {
        "claude-code": AgentMark("✻", on_dark="#DF8B70", on_light="#AA4726"),
        "codex": AgentMark(">_", on_dark="#6BA1F8", on_light="#0B5EE7"),
    }


@pytest.mark.parametrize("adapter", MARKED, ids=lambda adapter: adapter.id)
def test_every_character_of_a_mark_is_one_cell_wherever_it_is_drawn(adapter: AgentAdapter) -> None:
    """One cell by Rich's ``cell_len`` (what the table pads by), and neither wide nor
    East-Asian-ambiguous (``A``): a CJK locale or font draws an ambiguous character two cells
    wide, and every column after the mark would move. Nor a space, a combining mark or a
    format character, which draw nothing of their own."""
    glyph = _mark(adapter).glyph
    assert 1 <= len(glyph) <= 3, glyph
    for character in glyph:
        name = unicodedata.name(character)
        assert cell_len(character) == 1, name
        assert unicodedata.east_asian_width(character) in {"N", "Na", "H"}, name
        assert character.isprintable() and not character.isspace(), name
        assert unicodedata.category(character) not in {"Mn", "Me", "Cf"}, name


def test_the_marks_differ_by_shape_and_by_colour() -> None:
    """By shape, so they read apart with no colour (NO_COLOR, a mono theme); by colour, so
    they read apart at a glance, on a dark theme and on a light one."""
    assert len(MARKED) == len(adapters()) == 2
    assert len({_mark(adapter).glyph for adapter in MARKED}) == len(MARKED)
    for dark in (True, False):
        assert len({_mark(adapter).colour(dark).lower() for adapter in MARKED}) == len(MARKED)


def _hue(colour: str) -> float:
    parsed = Color.parse(colour)
    return colorsys.rgb_to_hls(parsed.r / 255, parsed.g / 255, parsed.b / 255)[0] * 360


@pytest.mark.parametrize("adapter", MARKED, ids=lambda adapter: adapter.id)
def test_a_marks_two_colours_are_one_hue_lighter_on_a_dark_theme(adapter: AgentAdapter) -> None:
    """Orange stays orange and blue stays blue: the pair differs in lightness alone, the
    lighter one for a dark theme. That each reads at 3:1 on the surfaces it is drawn on is
    measured on screen, theme by theme (``tests/test_ui_agent_mark_contrast.py``)."""
    mark = _mark(adapter)
    assert abs(_hue(mark.on_dark) - _hue(mark.on_light)) < 3, (mark.on_dark, mark.on_light)
    assert Color.parse(mark.on_dark).brightness > Color.parse(mark.on_light).brightness
    assert (mark.colour(True), mark.colour(False)) == (mark.on_dark, mark.on_light)


@pytest.fixture
def lettered() -> Iterator[None]:
    """Two agents with account slots and no mark: the Fake Agent, and one whose label every
    start of which Codex's label shares up to its whole (``Codexy``, six letters)."""

    class Codexy(FakeAgentAdapter):
        id = "codexy-agent"
        label = "Codexy"

    added = (FakeAgentAdapter(), Codexy())
    for adapter in added:
        register(adapter)
    try:
        yield
    finally:
        for adapter in added:
            unregister(adapter.id)


@pytest.mark.usefixtures("lettered")
def test_an_agent_with_no_mark_keeps_its_letters_and_every_mark_pads_to_the_widest() -> None:
    marks = accounts_view.agent_marks()
    assert {agent: (mark.plain, str(mark.style)) for agent, mark in marks.items()} == {
        "claude-code": ("✻     ", "#DF8B70"),
        "codex": (">_    ", "#6BA1F8"),
        "fake-agent": ("Fa    ", "dim"),
        "codexy-agent": ("Codexy", "dim"),
    }
    assert {mark.cell_len for mark in marks.values()} == {6}


def test_the_two_agents_marks_take_two_cells_each() -> None:
    """``✻`` is one cell and ``>_`` two: padded, both take two, so the names after them start
    in one column and keep the 9 cells the letters left them."""
    marks = accounts_view.agent_marks()
    assert [cell_len(_mark(adapter).glyph) for adapter in MARKED] == [1, 2]
    assert {agent: mark.cell_len for agent, mark in marks.items()} == {"claude-code": 2, "codex": 2}


def test_an_account_of_an_agent_no_adapter_has_is_marked_by_its_ids_start() -> None:
    """An account a later release's agent left in the registry: its id's first two
    characters, dim, padded as the others are."""
    mark = accounts_view.agent_mark("later-agent")
    assert (mark.plain, str(mark.style), mark.cell_len) == ("la", "dim", 2)


def _status(slot: int, agent: str) -> ClaudeAccountStatus:
    account = ClaudeAccount(
        slot=slot,
        config_dir=accounts_core.accounts_root(agent) / str(slot),
        managed=True,
        agent=agent,
    )
    return ClaudeAccountStatus(
        account=account,
        label=f"account {slot}",
        identity=ClaudeIdentity(email=f"slot{slot}@example.com"),
        signed_in=True,
        subscription="max",
    )


def test_the_picker_draws_each_agents_mark_in_its_colour_before_its_name() -> None:
    """The account picker's rows where it lists both agents' accounts: the mark before the
    agent's name, in the mark's colour; nothing else of the row styled."""
    for status, glyph, colour, named in (
        (_status(2, "claude-code"), "✻", "#DF8B70", "✻ Claude Code"),
        (_status(5, "codex"), ">_", "#6BA1F8", ">_ Codex"),
    ):
        slot = status.account.slot
        prompt = account_prompt(status, agent=True)
        assert prompt.plain == f"{slot} · {named} · slot{slot}@example.com · max"
        assert prompt.plain == account_text(status, agent=True)
        styled = [(prompt.plain[span.start : span.end], str(span.style)) for span in prompt.spans]
        assert styled == [(glyph, colour)], styled


def test_a_list_of_one_agents_accounts_draws_its_rows_as_0_9_2_did() -> None:
    """No agent named, no mark, nothing styled: 0.9.2's ``2 · me@… · max``."""
    status = _status(2, "claude-code")
    assert account_text(status) == "2 · slot2@example.com · max"
    assert account_prompt(status).spans == []


def test_the_picker_marks_its_rows_only_where_it_lists_more_than_one_agents_accounts() -> None:
    """A Claude-only machine's picker draws no row of its own (``marked_rows`` is empty), so
    every row is its text as 0.9.2 drew it; with a Codex account too, every account row is
    drawn with its agent's mark."""

    def marked(*statuses: ClaudeAccountStatus) -> dict[str, str]:
        app = SimpleNamespace(current_theme=SimpleNamespace(dark=True))
        picker = cast(Any, SimpleNamespace(targets=Targets(accounts=list(statuses)), app=app))
        return {key: text.plain for key, text in AttachTargetScreen.marked_rows(picker).items()}

    claude = (_status(1, "claude-code"), _status(2, "claude-code"))
    assert marked(*claude) == {}
    assert marked(*claude, _status(5, "codex")) == {
        "account:1": "1 · ✻ Claude Code · slot1@example.com · max",
        "account:2": "2 · ✻ Claude Code · slot2@example.com · max",
        "account:5": "5 · >_ Codex · slot5@example.com · max",
    }


ACCOUNTS_TITLE_092 = "Accounts — spawn on a Claude account"
"""The picker's accounts section title in 0.9.2 (6a77e650, ``cli/ui/attach.py:84``)."""


def test_the_pickers_accounts_title_is_0_9_2s_while_it_lists_one_agents_accounts() -> None:
    """The R16-C verify (seq 11914): "Accounts — spawn on an account" had reached Claude-only
    machines, unnamed. One agent's accounts, or none read, name that agent's account, by its
    noun (``AgentAccounts.noun``): 0.9.2's title, byte for byte, for Claude Code's. A list of
    more than one agent's says "an account"."""
    claude = [_status(1, "claude-code"), _status(2, "claude-code")]
    assert accounts_title(claude) == ACCOUNTS_TITLE_092
    assert accounts_title(None) == accounts_title([]) == ACCOUNTS_TITLE_092
    assert accounts_title([_status(5, "codex")]) == "Accounts — spawn on a Codex account"
    assert accounts_title([*claude, _status(5, "codex")]) == "Accounts — spawn on an account"
