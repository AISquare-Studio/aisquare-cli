"""Doctor on a machine with no Node: no row goes amber over it.

The codebase snapshot is gone, and with it the doctor's rows for its packer, the Node
that packer ran on, and the tokenizer that counted its packs. Node is now an opt-in of
``install.sh``'s, for the Chrome DevTools MCP, so a machine without one made a choice,
not a mistake. ``browser tools`` may still name ``npx`` in its advice: it is green, and
it is about a tool the user picks.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from aisquare.models import CheckStatus
from aisquare.services import diagnostics

_NODE = ("node", "npx", "npm")


def test_no_row_is_about_node_or_the_removed_snapshot(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = shutil.which

    def without_node(name: str, *args: Any, **kwargs: Any) -> str | None:
        return None if name in _NODE else real(name, *args, **kwargs)

    monkeypatch.setattr("aisquare.services.diagnostics.shutil.which", without_node)
    checks = diagnostics.doctor()

    assert not {check.name for check in checks} & {"snapshot", "tiktoken", "node"}
    for check in checks:
        if check.status is CheckStatus.ok:
            continue
        said = f"{check.detail} {check.fix or ''}"
        assert "Node" not in said and "npx" not in said, check
