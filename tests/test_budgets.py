"""``tests/budgets.py``: a test's wall-clock budget, scaled on Windows only."""

from __future__ import annotations

import pytest

from tests.budgets import WINDOWS_FACTOR, budget


def test_a_budget_is_scaled_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tests.budgets.sys.platform", "win32")
    assert budget(1.0) == WINDOWS_FACTOR
    assert budget(0.2) == pytest.approx(0.2 * WINDOWS_FACTOR)


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_a_budget_is_what_the_test_wrote_everywhere_else(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    """The Linux legs keep the budgets they were measured against."""
    monkeypatch.setattr("tests.budgets.sys.platform", platform)
    assert budget(1.0) == 1.0
    assert budget(180.0) == 180.0
