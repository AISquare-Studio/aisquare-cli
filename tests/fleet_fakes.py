"""A fake fleet listing for the tests that script what ``fleet_service.list_agents`` answers.

``asq``'s tick reads the fleet through ``read_listings`` and ``list_agents_many`` (#83), not
``list_agents`` project by project, so a test that scripts ``list_agents`` alone scripts
nothing the sidebar shows. :func:`list_agents_as` scripts all three from one fake.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence

import pytest

from aisquare.core.store import ContextStore
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
