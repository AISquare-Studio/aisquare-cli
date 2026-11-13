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
colour reads on every Textual theme with a background of its own, the padding that keeps the
names after the marks in one column, and the account picker's rows. The Accounts page itself,
NO_COLOR and a Claude-only machine's unchanged screens are pinned in ``tests/test_ui_accounts.py``.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterator

import pytest
from rich.cells import cell_len
from textual.color import Color
from textual.theme import BUILTIN_THEMES

from aisquare.cli.ui.attach import account_prompt, account_text
from aisquare.cli.ui.views import accounts as accounts_view
from aisquare.core import claude_accounts as accounts_core
from aisquare.core.agent_adapters import adapters, register, unregister
from aisquare.core.agent_adapters.types import AgentAdapter, AgentMark
from aisquare.models import ClaudeAccount, ClaudeAccountStatus, ClaudeIdentity
from tests.fake_agent import FakeAgentAdapter

MARKED: tuple[AgentAdapter, ...] = tuple(adapter for adapter in adapters() if adapter.mark)

#: Every built-in theme that paints a background of its own, by name: the ``ansi`` ones paint
#: the terminal's, which no colour can be measured against.
THEMES = {
    name: Color.parse(background)
    for name, theme in BUILTIN_THEMES.items()
    if not (background := theme.to_color_system().generate()["background"]).startswith("ansi")
}


def _mark(adapter: AgentAdapter) -> AgentMark:
    assert adapter.mark is not None
    return adapter.mark


def test_claude_code_and_codex_carry_the_marks_their_own_tuis_show() -> None:
    assert {adapter.id: adapter.mark for adapter in adapters()} == {
        "claude-code": AgentMark("✻", "#D15C36"),
        "codex": AgentMark(">_", "#317BF6"),
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
    they read apart at a glance."""
    assert len(MARKED) == len(adapters()) == 2
    assert len({_mark(adapter).glyph for adapter in MARKED}) == len(MARKED)
    assert len({_mark(adapter).colour.lower() for adapter in MARKED}) == len(MARKED)


def _luminance(colour: Color) -> float:
    def channel(value: int) -> float:
        share = value / 255
        return share / 12.92 if share <= 0.03928 else ((share + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(colour.r) + 0.7152 * channel(colour.g) + 0.0722 * channel(colour.b)


def contrast(one: Color, other: Color) -> float:
    """WCAG's contrast ratio of two colours, 1 to 21."""
    light, dark = sorted((_luminance(one), _luminance(other)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


@pytest.mark.parametrize("adapter", MARKED, ids=lambda adapter: adapter.id)
def test_a_marks_colour_reads_at_three_to_one_on_every_theme(adapter: AgentAdapter) -> None:
    """WCAG's 3:1 for a graphical object, on each theme's own background, light ones
    included: Claude's own orange, #D97757, reads at 2.4:1 on textual-light, so Claude Code's
    is the nearest colour of its hue that reads at 3:1 everywhere."""
    colour = Color.parse(_mark(adapter).colour)
    assert len(THEMES) >= 19 and "textual-light" in THEMES and "textual-dark" in THEMES
    worst = min(THEMES, key=lambda name: contrast(colour, THEMES[name]))
    assert contrast(colour, THEMES[worst]) >= 3.0, (worst, contrast(colour, THEMES[worst]))


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
        "claude-code": ("✻     ", "#D15C36"),
        "codex": (">_    ", "#317BF6"),
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
        (_status(2, "claude-code"), "✻", "#D15C36", "✻ Claude Code"),
        (_status(5, "codex"), ">_", "#317BF6", ">_ Codex"),
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
