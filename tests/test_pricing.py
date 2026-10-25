"""The price table: dated, exact where Claude Code is exact, silent where it does not know."""

from __future__ import annotations

import pytest

from aisquare.core import pricing
from aisquare.core.pricing import RequestTokens


def test_list_price_matches_claude_codes_own_cost_for_a_real_run() -> None:
    """``claude -p`` on Haiku 5.5, 2026-10-23: ``modelUsage.costUSD`` was 0.00146363 for
    2 input, 4 output, 11,803 cache-read and 6,717 one-hour cache-write tokens."""
    tokens = RequestTokens(input=2, cache_write_1h=6_717, cache_read=11_803, output=4)
    assert pricing.cost_usd("claude-haiku-5-5", tokens) == pytest.approx(0.00146363, abs=1e-11)


def test_cache_writes_cost_more_for_the_longer_ttl() -> None:
    five = pricing.cost_usd("claude-sonnet-5-5", RequestTokens(cache_write_5m=1_000_000))
    hour = pricing.cost_usd("claude-sonnet-5-5", RequestTokens(cache_write_1h=1_000_000))
    assert five == pytest.approx(2.0 * 1.25)
    assert hour == pytest.approx(2.0 * 2.0)


def test_sonnet_5_5_matches_what_claude_code_billed_for_a_headless_run() -> None:
    """A ``claude -p`` run on Sonnet 5.5 (2026-10-23): Claude Code reported 0.0546114 for
    these tokens. Its cache reads are $0.10/MTok; at the $0.20 the source lists the
    run would come to 0.0645 — 18% over, which is how the rate was found."""
    tokens = RequestTokens(input=10, cache_write_1h=8_885, cache_read=98_614, output=919)
    assert pricing.cost_usd("claude-sonnet-5-5", tokens) == pytest.approx(0.0546114, abs=1e-9)


def test_cache_reads_use_the_models_own_discount() -> None:
    million_read = RequestTokens(cache_read=1_000_000)
    assert pricing.cost_usd("claude-sonnet-5-5", million_read) == pytest.approx(0.10)
    assert pricing.cost_usd("claude-opus-5-5", million_read) == pytest.approx(0.20)
    assert pricing.cost_usd("claude-fable-5-1", million_read) == pytest.approx(0.25)


def test_haiku_5_5_bills_a_long_prompt_at_the_long_rate() -> None:
    short = RequestTokens(input=100_000, output=1_000_000)
    long = RequestTokens(input=100_001, output=1_000_000)
    assert pricing.cost_usd("claude-haiku-5-5", short) == pytest.approx(0.01 + 0.50)
    assert pricing.cost_usd("claude-haiku-5-5", long) == pytest.approx(0.0500005 + 2.50)


def test_weighted_tokens_use_the_fixed_multipliers_and_the_output_ratio() -> None:
    tokens = RequestTokens(
        input=10, cache_write_5m=100, cache_write_1h=100, cache_read=1_000, output=4
    )
    # 10 + 1.25 x 200 + 0.1 x 1000 + (10 / 2) x 4
    assert pricing.weighted_tokens("claude-sonnet-5-5", tokens) == pytest.approx(380.0)


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("claude-opus-5-5[1m]", "claude-opus-5-5"),
        ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
        ("us.anthropic.claude-opus-5-5", "claude-opus-5-5"),
        ("Claude-Sonnet-5-5", "claude-sonnet-5-5"),
    ],
)
def test_model_ids_are_priced_by_their_bare_name(raw: str, canonical: str) -> None:
    assert pricing.canonical_model(raw) == canonical
    assert pricing.price_for(raw) is pricing.PRICES[canonical]


def test_a_model_the_table_does_not_have_is_unpriced_not_guessed() -> None:
    tokens = RequestTokens(input=1_000)
    assert pricing.price_for("claude-opus-4-5") is None
    assert pricing.cost_usd("claude-opus-4-5", tokens) is None
    assert pricing.weighted_tokens("some-other-model", tokens) is None


def test_the_table_says_when_it_was_true() -> None:
    assert pricing.PRICES_AS_OF == "2026-10-06"
    assert "claude-api skill" in pricing.PRICES_SOURCE
