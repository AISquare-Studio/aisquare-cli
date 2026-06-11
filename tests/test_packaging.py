"""Both console scripts point at the same CLI entry point."""

from __future__ import annotations

from importlib.metadata import entry_points


def test_console_scripts_registered() -> None:
    scripts = {ep.name: ep.value for ep in entry_points(group="console_scripts")}
    assert scripts.get("aisquare") == "aisquare.cli.app:main"
    assert scripts.get("asq") == "aisquare.cli.app:main"
