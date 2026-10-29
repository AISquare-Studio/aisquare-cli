"""A fake fleet listing for the tests that script what ``fleet_service.list_agents`` answers.

``asq``'s tick reads the fleet through ``read_listings`` and ``list_agents_many`` (#83), not
``list_agents`` project by project, so a test that scripts ``list_agents`` alone scripts
nothing the sidebar shows. :func:`list_agents_as` scripts all three from one fake.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from pathlib import Path

import pytest

from aisquare.core.config import FleetSettings
from aisquare.core.store import ContextStore
from aisquare.core.tmux import Completed, TmuxServer
from aisquare.models import FleetAgentStatus, ProjectInfo
from aisquare.services import fleet as fleet_service

Fake = Callable[..., list[FleetAgentStatus]]


def list_agents_as(monkeypatch: pytest.MonkeyPatch, fake: Fake) -> None:
    """``fake(project)`` answers ``list_agents`` and every project of ``list_agents_many``.

    Every listed project is read, a collapsed group's hidden members bar (``skip``), and
    none counts live rows: the fake's project has no store rows to count.
    """

    def read(
        store: ContextStore, projects: Sequence[ProjectInfo], *, skip: Collection[str] = ()
    ) -> fleet_service.Listings:
        listings = {
            project.id: fleet_service._Listing(project, [], {}, {})
            for project in projects
            if project.id not in skip
        }
        return fleet_service.Listings(listings, {})

    def many(listings: fleet_service.Listings) -> fleet_service.ManyAgents:
        agents: dict[str, list[FleetAgentStatus]] = {}
        errors: dict[str, Exception] = {}
        for project_id, listing in listings.listings.items():
            try:
                agents[project_id] = fake(listing.project)
            except Exception as exc:  # as list_agents_many: the project's, said
                errors[project_id] = exc
        return fleet_service.ManyAgents(agents, errors)

    monkeypatch.setattr(fleet_service, "list_agents", fake)
    monkeypatch.setattr(fleet_service, "read_listings", read)
    monkeypatch.setattr(fleet_service, "list_agents_many", many)


class SessionsOnly(TmuxServer):
    """A fleet tmux server that holds named sessions and nothing else, and runs no tmux: what
    a project rename asks (``has_session``, ``rename_session``) in a UI test, whose guards
    refuse every real tmux command."""

    def __init__(self, sessions: Collection[str] = ()) -> None:
        super().__init__("asq-test-sessions", runner=self._refused)
        self.sessions = set(sessions)
        self.renamed: list[tuple[str, str]] = []

    @staticmethod
    def _refused(argv: Sequence[str], stdin: bytes | None) -> Completed:
        raise AssertionError(f"SessionsOnly ran tmux: {list(argv)}")

    def at(self, path: Path | None) -> TmuxServer:
        return self

    def has_session(self, name: str) -> bool:
        return name in self.sessions

    def rename_session(self, old: str, new: str) -> None:
        self.sessions.discard(old)
        self.sessions.add(new)
        self.renamed.append((old, new))


def tmux_sessions_as(
    monkeypatch: pytest.MonkeyPatch, sessions: Collection[str] = ()
) -> SessionsOnly:
    """Every fleet server the service asks for is one :class:`SessionsOnly` holding
    ``sessions``."""
    fake = SessionsOnly(sessions)

    def factory(config: FleetSettings | None = None) -> TmuxServer:
        return fake

    monkeypatch.setattr(fleet_service, "server", factory)
    return fake
