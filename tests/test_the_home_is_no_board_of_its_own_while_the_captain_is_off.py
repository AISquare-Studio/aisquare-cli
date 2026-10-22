"""With the experimental captain off, the aisquare home is a captured row like any other.

Review of #240, F7. ``project.tracked`` let the home's row write whatever ``[projects]
capture`` said, because the home is the captain's home board, captured on purpose and never
onboarded. It let it whatever the captain switch said, too. So once a home row was left
behind (by a run with the captain on, or a session while capture was on), a Claude session
started in the home registered there and was briefed on the home board with the captain
off, where main 0.8.2 stays silent. Off, the home is untracked with capture off, as on main.
On, it is the captain's board as before.
"""

from __future__ import annotations

import pytest

from aisquare.core import experimental
from aisquare.core.store import store_session
from aisquare.services import project as project_service
from aisquare.services import team as team_service
from aisquare.services.captain import state as captain_state

SESSION = "eeee5555-0000-0000-0000-000000000000"


@pytest.fixture
def home_row_left(monkeypatch: pytest.MonkeyPatch) -> object:
    """Capture off (0.8.1's default), and the home's row left by a run with the captain on."""
    monkeypatch.setattr(project_service, "capture_on", lambda: False)
    return captain_state.home_project()


def test_off_the_home_row_is_not_tracked(home_row_left: object, captain_off: None) -> None:
    assert not experimental.captain_enabled()
    with store_session() as store:
        assert project_service.tracked(store, captain_state.home_project()) is False


def test_off_a_session_started_in_the_home_registers_and_is_briefed_nothing(
    home_row_left: object, captain_off: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    home = captain_state.home_project()

    said = team_service.hook_session_start(SESSION, home.root, "startup")

    assert said == ""
    with store_session() as store:
        assert store.get_session(SESSION) is None


@pytest.mark.needs_captain
def test_on_the_home_row_is_the_captains_board_whatever_capture_says(
    home_row_left: object,
) -> None:
    """The control: on (the suite's ``captain_on``), the home's row is tracked."""
    assert experimental.captain_enabled()
    with store_session() as store:
        assert project_service.tracked(store, captain_state.home_project()) is True
