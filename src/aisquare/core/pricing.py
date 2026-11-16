"""What a token costs, per model: a dated table, and nothing guessed past it.

Two figures come out of here, and they answer different questions:

**Cost-weighted tokens** — input-token equivalents at the request's own model:
``input + 1.25 x cache writes + 0.1 x cache reads + (output price / input
price) x output``. The multipliers are FIXED, the same for every model, so two
sessions on one model compare in it to within the model's own cache-read
discount.

**USD** — the list price of every request, from :data:`PRICES`: cache writes at
1.25x input for the 5-minute TTL and 2x for the 1-hour one (the usage block
says which), cache reads at the model's own multiplier (0.1x for most, 0.05x on
Claude Opus 5.5 and Claude Sonnet 5.5, 0.025x on Claude Fable 5.1), and Claude
Haiku 5.5's dearer rate for prompts over 100K tokens applied per request, as the
API bills it. Checked against Claude Code's own ``total_cost_usd``
(``costBasis: list``) on this machine, 2026-10-23: Haiku 5.5 identical to the last
digit; Sonnet 5.5 fitted over 96 headless runs with zero residual, which is how
its cache read came out at $0.10/MTok where the source says $0.20 — the table
follows what Claude Code bills.

A model missing from the table is UNPRICED, not priced like its neighbour: its
tokens are counted raw and reported apart, and neither figure includes them.
A price invented for a model nobody looked up is a number that survives into a
comparison.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PRICES_AS_OF = "2026-10-06"
PRICES_SOURCE = (
    "Anthropic first-party list prices: the Current Models table and prompt-caching "
    "economics of the claude-api skill bundled with Claude Code 2.1.309 (cached 2026-10-06)"
)

#: The multipliers of the cost-weighted unit, fixed for every model.
WEIGHT_CACHE_WRITE = 1.25
WEIGHT_CACHE_READ = 0.1

#: Cache-write multipliers of the USD price, by TTL.
CACHE_WRITE_5M = 1.25
CACHE_WRITE_1H = 2.0


@dataclass(frozen=True)
class Price:
    """One model's list price, USD per million tokens."""

    input: float
    output: float
    cache_read: float = 0.1
    """A multiple of the input price, as the pricing page states it."""
    long_prompt_over: int | None = None
    """Prompt size (input + cache reads + cache writes) above which ``long_*`` apply."""
    long_input: float | None = None
    long_output: float | None = None
    cache_write: float | None = CACHE_WRITE_5M
    """A cache write of the shorter TTL, a multiple of the input price (Anthropic's 5-minute
    1.25x, its 1-hour write being :data:`CACHE_WRITE_1H`); ``None`` where the source lists
    no cache-write price, which leaves a request that wrote to the cache unpriced. Last, so
    main's positional order of the fields before it holds."""

    def rates(self, prompt_tokens: int) -> tuple[float, float]:
        """``(input, output)`` per MTok for a request whose prompt is ``prompt_tokens``."""
        if (
            self.long_prompt_over is not None
            and prompt_tokens > self.long_prompt_over
            and self.long_input is not None
            and self.long_output is not None
        ):
            return self.long_input, self.long_output
        return self.input, self.output


PRICES: dict[str, Price] = {
    "claude-fable-5-1": Price(10.0, 50.0, cache_read=0.025),
    "claude-mythos-5-1": Price(10.0, 50.0, cache_read=0.025),
    "claude-fable-5": Price(10.0, 50.0),
    "claude-mythos-5": Price(10.0, 50.0),
    "claude-opus-5-5": Price(4.0, 20.0, cache_read=0.05),
    "claude-opus-5": Price(5.0, 25.0),
    "claude-opus-4-8": Price(5.0, 25.0),
    "claude-opus-4-7": Price(5.0, 25.0),
    "claude-opus-4-6": Price(5.0, 25.0),
    # Cache reads at $0.10/MTok, measured (module docstring); the source lists $0.20.
    "claude-sonnet-5-5": Price(2.0, 10.0, cache_read=0.05),
    "claude-sonnet-5": Price(2.0, 10.0),
    "claude-sonnet-4-6": Price(3.0, 15.0),
    "claude-haiku-5-5": Price(
        0.10, 0.50, long_prompt_over=100_000, long_input=0.50, long_output=2.50
    ),
    "claude-haiku-4-5": Price(1.0, 5.0),
}

_DATE_SUFFIX = re.compile(r"-\d{8}$")
_CONTEXT_SUFFIX = re.compile(r"\[[^\]]*\]$")


def canonical_model(model: str) -> str:
    """``model`` as :data:`PRICES` keys it: no ``[1m]``, no date, no platform prefix.

    ``claude-haiku-4-5-20251001`` and ``us.anthropic.claude-opus-5-5`` are the
    same price as their bare ids; the context-window suffix Claude Code puts on
    a model id (``claude-opus-5-5[1m]``) is not part of the model at all.
    """
    name = _CONTEXT_SUFFIX.sub("", model.strip().lower())
    start = name.find("claude-")
    if start > 0:
        name = name[start:]
    return _DATE_SUFFIX.sub("", name)


def price_for(model: str) -> Price | None:
    """The list price of ``model``, or ``None`` when the table does not have it."""
    return PRICES.get(canonical_model(model))


@dataclass(frozen=True)
class RequestTokens:
    """The four billed token kinds of ONE request, cache writes split by TTL."""

    input: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0
    output: int = 0

    @property
    def cache_write(self) -> int:
        return self.cache_write_5m + self.cache_write_1h

    @property
    def prompt(self) -> int:
        return self.input + self.cache_write + self.cache_read

    @property
    def total(self) -> int:
        return self.prompt + self.output


def cost_usd(model: str, tokens: RequestTokens, *, prompt: int | None = None) -> float | None:
    """The list price of one request in USD, or ``None`` for an unpriced model.

    ``prompt`` is the request's whole prompt where ``tokens`` are only part of it (an
    increase a later line of the same request added): the tier is the request's."""
    return cost_at(price_for(model), tokens, prompt=prompt)


def weighted_tokens(
    model: str, tokens: RequestTokens, *, prompt: int | None = None
) -> float | None:
    """Cost-weighted tokens of one request (see the module docstring), or ``None``
    for an unpriced model — the output weight IS a price ratio. ``prompt`` as for
    :func:`cost_usd`."""
    return weighted_at(price_for(model), tokens, prompt=prompt)


def cost_at(
    price: Price | None, tokens: RequestTokens, *, prompt: int | None = None
) -> float | None:
    """:func:`cost_usd` at ``price`` (an agent's own table's, ``AgentAdapter.price_table``):
    ``None`` with no price, and for a request that wrote to the cache at a price that
    lists no cache write."""
    if price is None or (tokens.cache_write and price.cache_write is None):
        return None
    input_rate, output_rate = price.rates(tokens.prompt if prompt is None else prompt)
    per_mtok = (
        tokens.input * input_rate
        + tokens.cache_write_5m * input_rate * (price.cache_write or 0.0)
        + tokens.cache_write_1h * input_rate * CACHE_WRITE_1H
        + tokens.cache_read * input_rate * price.cache_read
        + tokens.output * output_rate
    )
    return per_mtok / 1_000_000


def weighted_at(
    price: Price | None, tokens: RequestTokens, *, prompt: int | None = None
) -> float | None:
    """:func:`weighted_tokens` at ``price``; ``None`` with no price. The weights are fixed,
    whatever the price lists of cache writes."""
    if price is None:
        return None
    input_rate, output_rate = price.rates(tokens.prompt if prompt is None else prompt)
    return (
        tokens.input
        + WEIGHT_CACHE_WRITE * tokens.cache_write
        + WEIGHT_CACHE_READ * tokens.cache_read
        + (output_rate / input_rate) * tokens.output
    )
